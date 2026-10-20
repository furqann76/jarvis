"""Home page: hero (orb + greeting + command bar) and the live task timeline."""

import re
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jarvis.models.schemas import Task, TaskStatus, TaskStep
from jarvis.ui.dashboard import theme
from jarvis.ui.dashboard.icons import icon
from jarvis.ui.dashboard.widgets import (
    ElidedLabel,
    FlowLayout,
    IconBadge,
    Orb,
    Separator,
    Waveform,
    card_header,
    icon_label,
    label,
)

SUGGESTIONS = ["Open VS Code", "Find my project files", "Search for Python jobs",
               "Organize my downloads"]
FEATURES = [
    ("files", "folder", "Manage Files", "Find, move, delete,\norganize"),
    ("apps", "grid", "Control Apps", "Open and operate\napplications"),
    ("browser", "globe", "Browse Web", "Search, research,\nautomate"),
    ("system", "terminal", "System Tasks", "Look at the screen,\nkeyboard & mouse"),
]


def greeting(now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    if hour < 12:
        return "Good morning!"
    if hour < 17:
        return "Good afternoon!"
    return "Good evening!"


class CommandBar(QFrame):
    """Pill-shaped input with a push-to-talk mic button and send/stop."""

    submitted = Signal(str)
    cancel_requested = Signal()
    mic_toggled = Signal(bool)  # True = start recording, False = stop and transcribe

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("commandBar")
        self.setFixedHeight(68)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 10, 10, 10)
        row.setSpacing(12)

        self.mic_button = QPushButton()
        self.mic_button.setObjectName("micButton")
        self.mic_button.setCheckable(True)
        self.mic_button.setFixedSize(46, 46)
        self.mic_button.setIcon(icon("mic", "white", 22))
        self.mic_button.setIconSize(QSize(22, 22))
        self.mic_button.setToolTip("Push to talk (Ctrl+Space). Click again to send.")
        self.mic_button.setCursor(Qt.CursorShape.PointingHandCursor)

        self.input = QLineEdit()
        self.input.setObjectName("commandInput")
        self.input.setPlaceholderText("Speak or type your command…")

        self.send_button = QPushButton()
        self.send_button.setObjectName("sendButton")
        self.send_button.setFixedSize(40, 40)
        self.send_button.setIcon(icon("arrow-right", theme.TEXT, 20))
        self.send_button.setToolTip("Send (Enter)")
        self.send_button.setCursor(Qt.CursorShape.PointingHandCursor)

        self.stop_button = QPushButton()
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setFixedSize(40, 40)
        self.stop_button.setIcon(icon("stop", theme.RED, 18))
        self.stop_button.setToolTip("Cancel the running task")
        self.stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_button.hide()

        row.addWidget(self.mic_button)
        row.addWidget(self.input, 1)
        row.addWidget(self.stop_button)
        row.addWidget(self.send_button)

        self.input.returnPressed.connect(self.submit)
        self.send_button.clicked.connect(lambda: self.submit())  # clicked passes a bool
        self.stop_button.clicked.connect(self.cancel_requested.emit)
        self.mic_button.toggled.connect(self._on_mic)
        self.busy = False

    def submit(self, text: str | None = None) -> None:
        text = (text if text is not None else self.input.text()).strip()
        if text and not self.busy:
            self.input.clear()
            self.submitted.emit(text)

    def _on_mic(self, recording: bool) -> None:
        self.mic_button.setIcon(icon("stop" if recording else "mic", "white", 22))
        self.mic_toggled.emit(recording)

    def toggle_mic(self) -> None:
        if self.mic_button.isChecked() or not self.busy:
            self.mic_button.toggle()

    def set_mic_state(self, recording: bool) -> None:
        self.mic_button.blockSignals(True)
        self.mic_button.setChecked(recording)
        self.mic_button.setIcon(icon("stop" if recording else "mic", "white", 22))
        self.mic_button.blockSignals(False)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.input.setEnabled(not busy)
        self.input.setPlaceholderText("Working on it…" if busy else "Speak or type your command…")
        self.send_button.setVisible(not busy)
        self.stop_button.setVisible(busy)
        self.mic_button.setEnabled(not busy or self.mic_button.isChecked())
        if not busy:
            self.input.setFocus()


class Hero(QFrame):
    feature_clicked = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("card")
        col = QVBoxLayout(self)
        col.setContentsMargins(22, 26, 22, 18)
        col.setSpacing(10)

        orb_row = QHBoxLayout()
        orb_row.addStretch(1)
        self.left_wave = Waveform(11, mirrored=False)
        self.orb = Orb(124)
        self.right_wave = Waveform(11, mirrored=True)
        for w in (self.left_wave, self.orb, self.right_wave):
            orb_row.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)
        orb_row.addStretch(1)
        col.addLayout(orb_row)

        self.greeting = label(greeting(), "greeting")
        self.greeting.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub = QLabel("I am <b>Jarvis</b>. How can I help you today?")
        sub.setObjectName("subGreeting")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(self.greeting)
        col.addWidget(sub)
        col.addSpacing(10)

        self.command = CommandBar()
        col.addWidget(self.command)

        chips_holder = QWidget()
        chips = FlowLayout(chips_holder, spacing=10)
        try_label = label("Try saying:", "muted")
        try_label.setMinimumHeight(30)
        chips.addWidget(try_label)
        for text in SUGGESTIONS:
            chip = QPushButton(text)
            chip.setObjectName("chip")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _=False, t=text: self.command.submit(t))
            chips.addWidget(chip)
        col.addWidget(chips_holder)
        col.addSpacing(6)

        grid = QGridLayout()
        grid.setSpacing(12)
        for i, (key, icon_name, title, text) in enumerate(FEATURES):
            button = QPushButton()
            button.setObjectName("featureCard")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(128)
            button.setMinimumWidth(110)
            inner = QVBoxLayout(button)
            inner.setSpacing(6)
            inner.addWidget(icon_label(icon_name, theme.ACCENT_HI, 34), 0,
                            Qt.AlignmentFlag.AlignHCenter)
            t = label(title)
            t.setStyleSheet("font-size: 15px; font-weight: 600; background: transparent;")
            t.setAlignment(Qt.AlignmentFlag.AlignCenter)
            d = label(text, "muted")
            d.setAlignment(Qt.AlignmentFlag.AlignCenter)
            d.setStyleSheet(f"color: {theme.MUTED}; font-size: 13px; background: transparent;")
            for w in (t, d):
                w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                inner.addWidget(w)
            button.clicked.connect(lambda _=False, k=key: self.feature_clicked.emit(k))
            grid.addWidget(button, 0, i)
        col.addLayout(grid)

    def set_state(self, state: str) -> None:
        for w in (self.left_wave, self.orb, self.right_wave):
            w.set_state(state)

    def refresh_greeting(self) -> None:
        self.greeting.setText(greeting())


# --------------------------------------------------------------------------- timeline

TOOL_LABELS = {
    "list_directory": ("Listing folder", "folder"),
    "search_files": ("Searching for files", "file-search"),
    "read_text_file": ("Reading file", "file"),
    "create_directory": ("Creating folder", "folder"),
    "trash_file": ("Moving to Trash", "trash"),
    "bulk_move_files": ("Moving files", "arrow-right"),
    "permanently_delete_files": ("Deleting files", "trash"),
    "list_applications": ("Finding applications", "grid"),
    "launch_application": ("Opening application", "grid"),
    "open_file": ("Opening file", "file"),
    "close_window": ("Closing window", "x-circle"),
    "web_search": ("Searching the web", "search"),
    "fetch_webpage": ("Reading web page", "globe"),
    "open_website": ("Opening website", "globe"),
    "browser_open": ("Opening page", "globe"),
    "browser_read": ("Reading page", "eye"),
    "browser_click": ("Clicking on page", "globe"),
    "browser_type": ("Typing on page", "keyboard"),
    "submit_web_form": ("Submitting form", "globe"),
    "list_windows": ("Checking windows", "monitor"),
    "get_active_window": ("Checking windows", "monitor"),
    "inspect_window": ("Reading window", "monitor"),
    "activate_element": ("Pressing buttons", "grid"),
    "set_element_text": ("Entering text", "keyboard"),
    "type_keyboard": ("Typing", "keyboard"),
    "press_keys": ("Pressing keys", "keyboard"),
    "look_at_screen": ("Looking at the screen", "eye"),
    "click_on_screen": ("Clicking", "monitor"),
    "mouse_click": ("Clicking", "monitor"),
    "scroll": ("Scrolling", "monitor"),
}

# status -> (label, colour, icon)
STATUS_STYLE = {
    "done": ("Completed", theme.GREEN, "check-circle"),
    "failed": ("Failed", theme.RED, "x-circle"),
    "cancelled": ("Cancelled", theme.AMBER, "x-circle"),
    "approval": ("Waiting for approval", theme.AMBER, "clock"),
    "running": ("In progress", theme.ACCENT_HI, "loader"),
    "pending": ("Pending", theme.DIM, "clock"),
}


@dataclass
class TimelineRow:
    key: str
    title: str
    subtitle: str
    icon: str
    status: str  # key of STATUS_STYLE


def _step_status(step: TaskStep) -> str:
    return {
        TaskStatus.COMPLETED: "done",
        TaskStatus.FAILED: "failed",
        TaskStatus.CANCELLED: "cancelled",
        TaskStatus.AWAITING_APPROVAL: "approval",
        TaskStatus.EXECUTING: "running",
        TaskStatus.VERIFYING: "running",
    }.get(step.status, "pending")


def timeline_rows(task: Task) -> list[TimelineRow]:
    """The stages shown for a task: understanding, one row per tool call, and the result."""
    terminal = task.status.is_terminal
    if task.steps or task.status == TaskStatus.COMPLETED:
        first = "done"
    elif task.status == TaskStatus.FAILED:
        first = "failed"
    elif task.status == TaskStatus.CANCELLED:
        first = "cancelled"
    else:
        first = "running"
    rows = [TimelineRow("understand", "Understanding your command",
                        f"“{_short(task.user_prompt, 70)}”", "mic", first)]
    for step in task.steps:
        name = step.tool_call.name if step.tool_call else ""
        title, icon_name = TOOL_LABELS.get(name, (name.replace("_", " ").capitalize(), "list"))
        status = _step_status(step)
        if status == "approval":
            title, icon_name = "Waiting for approval", "user"
        rows.append(TimelineRow(f"step{step.step_id}", title,
                                _short(humanize_step(step.description), 80), icon_name, status))
    final = {
        TaskStatus.COMPLETED: ("Task completed", "done"),
        TaskStatus.FAILED: ("Task failed", "failed"),
        TaskStatus.CANCELLED: ("Task cancelled", "cancelled"),
    }.get(task.status, ("Task completed", "pending"))
    subtitle = _short(task.final_response or "", 80) if terminal else "Working on it…"
    rows.append(TimelineRow("final", final[0], subtitle, "flag", final[1]))
    return rows


_CALL = re.compile(r"^\s*\w+\((?P<args>.*)\)\s*(?P<rest>.*)$", re.DOTALL)
_ARG_VALUE = re.compile(r"\w+=(?:'([^']*)'|\"([^\"]*)\"|(\[[^\]]*\])|([^,]+))")


def humanize_step(description: str) -> str:
    """'search_files(directory_path='~/Downloads', pattern='*') [ctx]' -> '~/Downloads · *'."""
    match = _CALL.match(description)
    if not match:
        return description
    values = [next(v for v in m.groups() if v is not None).strip()
              for m in _ARG_VALUE.finditer(match.group("args"))]
    text = " · ".join(v for v in values if v)
    rest = match.group("rest").strip().strip("[]")
    return f"{text} — {rest}" if text and rest else (text or rest or description)


def _short(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


class _TimelineItem(QWidget):
    def __init__(self, number: int, row: TimelineRow, time_text: str):
        super().__init__()
        line = QHBoxLayout(self)
        line.setContentsMargins(0, 6, 0, 6)
        line.setSpacing(14)
        num = QLabel(str(number))
        num.setFixedSize(26, 26)
        num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        num.setStyleSheet(f"background: {theme.ACCENT_SOFT}; color: #cfe0ff; border-radius: 13px;"
                          " font-size: 12px; font-weight: 600;")
        line.addWidget(num, 0, Qt.AlignmentFlag.AlignVCenter)

        label_text, color, status_icon = STATUS_STYLE[row.status]
        tint = {"done": theme.GREEN_SOFT, "approval": theme.AMBER_SOFT,
                "failed": theme.RED_SOFT, "cancelled": theme.AMBER_SOFT}.get(row.status,
                                                                              theme.PANEL_2)
        icon_color = {"done": theme.GREEN, "approval": theme.AMBER, "failed": theme.RED,
                      "cancelled": theme.AMBER, "running": theme.ACCENT_HI}.get(row.status,
                                                                                 theme.MUTED)
        line.addWidget(IconBadge(row.icon, icon_color, tint, 40, round_=True))

        texts = QVBoxLayout()
        texts.setSpacing(2)
        title = ElidedLabel(row.title)
        title.setStyleSheet("font-size: 14px; font-weight: 500;")
        sub = ElidedLabel(row.subtitle, "muted")
        sub.setStyleSheet(f"color: {theme.MUTED}; font-size: 12px;")
        texts.addWidget(title)
        texts.addWidget(sub)
        line.addLayout(texts, 1)

        right = QVBoxLayout()
        right.setSpacing(2)
        status = QHBoxLayout()
        status.setSpacing(5)
        status.addWidget(icon_label(status_icon, color, 14))
        status_text = label(label_text)
        status_text.setStyleSheet(f"color: {color}; font-size: 12px; font-weight: 600;")
        status.addWidget(status_text)
        right.addLayout(status)
        when = label(time_text or "--:--", "dim")
        when.setAlignment(Qt.AlignmentFlag.AlignRight)
        right.addWidget(when)
        holder = QWidget()
        holder.setFixedWidth(150)
        holder.setLayout(right)
        line.addWidget(holder)


class TimelineCard(QFrame):
    view_all = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("card")
        self.col = QVBoxLayout(self)
        self.col.setContentsMargins(20, 16, 20, 16)
        self.col.setSpacing(4)
        link = QPushButton("View All  ›")
        link.setObjectName("linkButton")
        link.setCursor(Qt.CursorShape.PointingHandCursor)
        link.clicked.connect(self.view_all.emit)
        self.col.addLayout(card_header("history", "Task Timeline", link))
        self.body = QVBoxLayout()
        self.body.setSpacing(0)
        self.col.addLayout(self.body)
        self._times: dict[tuple[str, str], str] = {}
        self._task_id: str | None = None
        self._show_empty()

    def _clear(self) -> None:
        while self.body.count():
            item = self.body.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _show_empty(self) -> None:
        self._clear()
        empty = label("No task yet. Say “Jarvis” or type a command above.", "muted")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setMinimumHeight(80)
        self.body.addWidget(empty)

    def update_task(self, task: Task) -> None:
        if task.id != self._task_id:
            self._task_id = task.id
        rows = timeline_rows(task)
        now = datetime.now().strftime("%I:%M %p").lstrip("0")
        for row in rows:
            key = (task.id, row.key)
            if row.status != "pending" and key not in self._times:
                self._times[key] = now
        self._clear()
        for i, row in enumerate(rows, start=1):
            self.body.addWidget(_TimelineItem(i, row, self._times.get((task.id, row.key), "")))
            if i < len(rows):
                self.body.addWidget(Separator())


class HomePage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)
        self.hero = Hero()
        self.timeline = TimelineCard()
        col.addWidget(self.hero)
        col.addWidget(self.timeline)
        col.addStretch(1)

