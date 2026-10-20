"""Right column: current activity, inline approvals, recent tasks, system info."""

import html
import os
import platform
import shutil
import subprocess
from datetime import UTC, datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from jarvis.models.schemas import Task, TaskStatus
from jarvis.ui.dashboard import theme
from jarvis.ui.dashboard.home import TOOL_LABELS
from jarvis.ui.dashboard.widgets import (
    IDLE,
    LISTENING,
    SPEAKING,
    THINKING,
    ActivityGlyph,
    ElidedLabel,
    IconBadge,
    badge,
    card_header,
    icon_label,
    label,
)

STATE_TEXT = {
    IDLE: ("Ready", "Say “Jarvis” or type below"),
    LISTENING: ("Listening…", "Say your command"),
    THINKING: ("Thinking…", "Working on your request"),
    SPEAKING: ("Speaking…", "Say “Jarvis” to interrupt"),
}


class ActivityCard(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("card")
        col = QVBoxLayout(self)
        col.setContentsMargins(18, 16, 18, 16)
        col.setSpacing(10)
        self.live = badge("◆ Live", "Green")
        col.addLayout(card_header("waves", "Current Activity", self.live))

        inner = QFrame()
        inner.setObjectName("subcard")
        box = QVBoxLayout(inner)
        box.setContentsMargins(14, 16, 14, 14)
        box.setSpacing(6)
        self.glyph = ActivityGlyph()
        box.addWidget(self.glyph, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title = label("Ready")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setStyleSheet("font-size: 16px; font-weight: 600; background: transparent;")
        self.detail = label("", "muted", wrap=True)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        box.addWidget(self.title)
        box.addWidget(self.detail)

        # The conversation: last request and reply (full history is in Task History).
        self.transcript = QTextBrowser()
        self.transcript.setOpenExternalLinks(False)
        self.transcript.setMinimumHeight(50)
        self.transcript.setMaximumHeight(130)
        self.transcript.hide()
        box.addWidget(self.transcript)
        col.addWidget(inner)
        self.set_state(IDLE)

    def set_state(self, state: str, detail: str | None = None) -> None:
        title, default_detail = STATE_TEXT[state]
        self.title.setText(title)
        self.detail.setText(detail if detail is not None else default_detail)
        self.glyph.set_state(state)
        self.live.setVisible(state != IDLE)

    def show_exchange(self, prompt: str, reply: str | None, status: TaskStatus | None) -> None:
        color = {TaskStatus.FAILED: theme.RED, TaskStatus.CANCELLED: theme.AMBER}.get(
            status, theme.ACCENT_HI)
        parts = [f'<p style="color:{theme.MUTED}">You: {html.escape(prompt)}</p>']
        if reply:
            body = html.escape(reply).replace("\n", "<br>")
            parts.append(f'<p><b style="color:{color}">Jarvis:</b> {body}</p>')
        self.transcript.setHtml("".join(parts))
        self.transcript.show()

    def add_notice(self, text: str) -> None:
        self.transcript.append(f'<p style="color:{theme.DIM}"><i>{html.escape(text)}</i></p>')
        self.transcript.show()


class ApprovalsCard(QFrame):
    """Inline Tier-2 approval (replaces the old modal dialog). Reject is the safe default."""

    decided = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("card")
        col = QVBoxLayout(self)
        col.setContentsMargins(18, 16, 18, 16)
        col.setSpacing(10)
        self.count = badge("None pending", "Grey")
        col.addLayout(card_header("shield", "Approvals", self.count))

        self.box = QFrame()
        self.box.setObjectName("subcard")
        box = QVBoxLayout(self.box)
        box.setContentsMargins(14, 14, 14, 14)
        box.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(12)
        self.badge_icon = IconBadge("folder", "#1b1300", theme.AMBER, 38)
        top.addWidget(self.badge_icon, 0, Qt.AlignmentFlag.AlignTop)
        self.headline = label("", wrap=True)
        self.headline.setStyleSheet("font-weight: 600; background: transparent;")
        top.addWidget(self.headline, 1)
        box.addLayout(top)
        self.details = label("", "muted", wrap=True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        self.details.setStyleSheet("color: #c3cee2; background: transparent; font-size: 13px;")
        box.addWidget(self.details)
        buttons = QHBoxLayout()
        self.approve = QPushButton("Approve")
        self.approve.setObjectName("primary")
        self.reject = QPushButton("Reject")
        self.reject.setObjectName("secondary")
        for b in (self.approve, self.reject):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            buttons.addWidget(b)
        box.addLayout(buttons)
        col.addWidget(self.box)
        self.empty = label("High-impact actions (deleting, moving many files, submitting "
                           "forms, typing in a terminal) wait here for your OK.", "dim", wrap=True)
        col.addWidget(self.empty)

        self.approve.clicked.connect(lambda: self.decided.emit(True))
        self.reject.clicked.connect(lambda: self.decided.emit(False))
        self.clear()

    def show_request(self, headline: str, prompt: str, arguments: dict, icon_name: str) -> None:
        self.count.setText("1 Pending")
        self.count.setObjectName("badgeAmber")
        self.count.style().polish(self.count)
        self.badge_icon.set_icon(icon_name, "#1b1300")
        self.headline.setText(headline)
        lines = [f"<span style='color:{theme.MUTED}'>For: “{html.escape(prompt)}”</span>"]
        for key, value in list(arguments.items())[:6]:
            shown = value if not isinstance(value, list) else ", ".join(map(str, value[:5])) + (
                f" … (+{len(value) - 5} more)" if len(value) > 5 else "")
            lines.append(f"• <b>{html.escape(key.replace('_', ' '))}</b>: "
                         f"{html.escape(str(shown))[:160]}")
        self.details.setText("<br>".join(lines))
        self.box.show()
        self.empty.hide()
        self.reject.setFocus()

    def clear(self) -> None:
        self.count.setText("None pending")
        self.count.setObjectName("badgeGrey")
        self.count.style().polish(self.count)
        self.box.hide()
        self.empty.show()


def task_icon(task: Task) -> str:
    for step in task.steps:
        if step.tool_call and step.tool_call.name in TOOL_LABELS:
            return TOOL_LABELS[step.tool_call.name][1]
    return "sparkles"


def relative_time(when: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    seconds = max(0, int((now - when).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        n = seconds // 60
        return f"{n} minute{'s' if n > 1 else ''} ago"
    if seconds < 86400:
        n = seconds // 3600
        return f"{n} hour{'s' if n > 1 else ''} ago"
    n = seconds // 86400
    return f"{n} day{'s' if n > 1 else ''} ago"


STATUS_BADGE = {
    TaskStatus.COMPLETED: ("Completed", theme.GREEN, "check-circle"),
    TaskStatus.FAILED: ("Failed", theme.RED, "x-circle"),
    TaskStatus.CANCELLED: ("Cancelled", theme.AMBER, "x-circle"),
}


class RecentTasksCard(QFrame):
    view_all = Signal()
    rerun = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("card")
        col = QVBoxLayout(self)
        col.setContentsMargins(18, 16, 18, 16)
        col.setSpacing(8)
        link = QPushButton("View All")
        link.setObjectName("linkButton")
        link.setCursor(Qt.CursorShape.PointingHandCursor)
        link.clicked.connect(self.view_all.emit)
        col.addLayout(card_header("history", "Recent Tasks", link))
        self.rows = QVBoxLayout()
        self.rows.setSpacing(6)
        col.addLayout(self.rows)

    def set_tasks(self, tasks: list[Task]) -> None:
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not tasks:
            self.rows.addWidget(label("Nothing yet.", "muted"))
            return
        for task in tasks[:5]:
            row = QPushButton()
            row.setObjectName("featureCard")
            row.setCursor(Qt.CursorShape.PointingHandCursor)
            row.setToolTip("Run this again")
            row.clicked.connect(lambda _=False, p=task.user_prompt: self.rerun.emit(p))
            line = QHBoxLayout(row)
            line.setContentsMargins(8, 6, 8, 6)
            line.setSpacing(12)
            line.addWidget(IconBadge(task_icon(task), theme.ACCENT_HI, theme.ACCENT_SOFT, 36))
            texts = QVBoxLayout()
            texts.setSpacing(1)
            title = ElidedLabel(task.user_prompt)
            title.setStyleSheet("font-size: 13px; background: transparent;")
            when = label(relative_time(task.created_at), "dim")
            when.setStyleSheet(f"color: {theme.DIM}; font-size: 12px; background: transparent;")
            texts.addWidget(title)
            texts.addWidget(when)
            line.addLayout(texts, 1)
            text, color, icon_name = STATUS_BADGE.get(task.status,
                                                      ("Running", theme.ACCENT_HI, "loader"))
            status = QHBoxLayout()
            status.setSpacing(4)
            status.addWidget(icon_label(icon_name, color, 14))
            s = label(text)
            s.setStyleSheet(f"color: {color}; font-size: 12px; background: transparent;")
            status.addWidget(s)
            line.addLayout(status)
            for child in row.findChildren(QWidget):
                child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.rows.addWidget(row)


def detect_system_info(llm: str, stt: str, tts: str) -> list[tuple[str, str, str]]:
    """(icon, label, value) rows. Blocking (runs nvidia-smi): call from a worker thread."""
    os_name = platform.system()
    try:
        with open("/etc/os-release") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    os_name = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    cpu = platform.processor() or "Unknown"
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    cpu = cpu.replace("(R)", "").replace("(TM)", "").replace("12th Gen ", "").strip()
    cpu = cpu.replace("Intel Core", "Intel")
    gpu = "None detected"
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
            if out:
                name, mem = [x.strip() for x in out[0].split(",")]
                gpu = f"{name.replace('NVIDIA GeForce ', '')} ({round(int(mem) / 1024)} GB)"
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return [("monitor", "OS", os_name), ("gpu", "GPU", gpu), ("cpu", "CPU", cpu),
            ("sparkles", "LLM", llm), ("mic", "STT", stt), ("volume", "TTS", tts),
            ("clock", "Cores", str(os.cpu_count() or "?"))]


class SystemInfoCard(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("card")
        col = QVBoxLayout(self)
        col.setContentsMargins(18, 16, 18, 16)
        col.setSpacing(8)
        col.addLayout(card_header("monitor", "System Info"))
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(8)
        col.addLayout(self.grid)

    def set_rows(self, rows: list[tuple[str, str, str]]) -> None:
        for i, (icon_name, name, value) in enumerate(rows):
            self.grid.addWidget(icon_label(icon_name, theme.MUTED, 16), i, 0)
            self.grid.addWidget(label(name, "muted"), i, 1)
            v = label(value)
            v.setAlignment(Qt.AlignmentFlag.AlignRight)
            v.setStyleSheet("font-size: 13px;")
            self.grid.addWidget(v, i, 2)
        self.grid.setColumnStretch(2, 1)


class RightPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)
        self.activity = ActivityCard()
        self.approvals = ApprovalsCard()
        self.recent = RecentTasksCard()
        self.system = SystemInfoCard()
        for w in (self.activity, self.approvals, self.recent, self.system):
            col.addWidget(w)
        col.addStretch(1)

