"""Secondary pages reached from the sidebar."""

import html
import subprocess
from pathlib import Path

from PySide6.QtCore import QDir, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileSystemModel,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from jarvis.models.schemas import Task, TaskStatus
from jarvis.ui.dashboard import theme
from jarvis.ui.dashboard.icons import icon
from jarvis.ui.dashboard.right_panel import STATUS_BADGE, relative_time
from jarvis.ui.dashboard.widgets import card_header, label


def _page(icon_name: str, title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
    page = QWidget()
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    from PySide6.QtWidgets import QFrame

    frame = QFrame()
    frame.setObjectName("card")
    col = QVBoxLayout(frame)
    col.setContentsMargins(22, 18, 22, 18)
    col.setSpacing(12)
    col.addLayout(card_header(icon_name, title))
    col.addWidget(label(subtitle, "muted", wrap=True))
    outer.addWidget(frame)
    return page, col


def _button(text: str, object_name: str = "accentButton", icon_name: str | None = None
            ) -> QPushButton:
    b = QPushButton(text)
    b.setObjectName(object_name)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon_name:
        b.setIcon(icon(icon_name, "white", 16))
    return b


class FileManagerPage(QWidget):
    """Browse the folders Jarvis may touch; ask Jarvis to act on the selection."""

    ask = Signal(str)

    def __init__(self, allowed_paths: list[str]):
        super().__init__()
        page, col = _page("folder", "File Manager",
                          "Jarvis can only work inside these folders (see Settings → "
                          "allowed paths). Double-click a file to open it.")
        QVBoxLayout(self).addWidget(page)
        self.layout().setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        self.root = QComboBox()
        self.root.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.root.setMinimumContentsLength(18)
        for raw in allowed_paths:
            path = Path(raw).expanduser()
            if path.exists():
                self.root.addItem(str(path))
        top.addWidget(self.root, 1)
        organize = _button("Organize with Jarvis", icon_name="sparkles")
        top.addWidget(organize)
        col.addLayout(top)

        self.model = QFileSystemModel(self)
        self.model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot)
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setAlternatingRowColors(True)
        self.tree.setMinimumHeight(520)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        col.addWidget(self.tree, 1)

        self.root.currentTextChanged.connect(self._set_root)
        organize.clicked.connect(
            lambda: self.ask.emit(f"Organize the files in {self.root.currentText()} into "
                                  "folders by type"))
        self.tree.doubleClicked.connect(self._open)
        if self.root.count():
            self._set_root(self.root.currentText())

    def _set_root(self, path: str) -> None:
        self.tree.setRootIndex(self.model.setRootPath(path))

    def _open(self, index) -> None:
        path = self.model.filePath(index)
        if Path(path).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))


class ApplicationsPage(QWidget):
    ask = Signal(str)

    def __init__(self, app_index) -> None:
        super().__init__()
        page, col = _page("grid", "Applications",
                          "Everything installed on this computer. Double-click to open, or "
                          "ask Jarvis to operate an app for you.")
        QVBoxLayout(self).addWidget(page)
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.index = app_index
        self.search = QLineEdit()
        self.search.setObjectName("field")
        self.search.setPlaceholderText("Search applications…")
        col.addWidget(self.search)
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setIconSize(QSize(34, 34))
        self.list.setGridSize(QSize(150, 92))
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setWordWrap(True)
        self.list.setMinimumHeight(520)
        col.addWidget(self.list, 1)
        self.search.textChanged.connect(self.refresh)
        self.list.itemDoubleClicked.connect(self._launch)
        self._loaded = False

    def showEvent(self, event) -> None:  # noqa: N802 - load lazily on first view
        if not self._loaded:
            self._loaded = True
            self.refresh()
        super().showEvent(event)

    def refresh(self) -> None:
        from PySide6.QtGui import QIcon

        self.list.clear()
        for entry in self.index.search(self.search.text() or None):
            item = QListWidgetItem(entry.name)
            app_icon = (QIcon(entry.icon) if entry.icon.startswith("/")
                        else QIcon.fromTheme(entry.icon or entry.id.removesuffix(".desktop")))
            item.setIcon(app_icon if not app_icon.isNull()
                         else icon("grid", theme.ACCENT_HI, 34))
            item.setToolTip(entry.comment or entry.name)
            item.setData(Qt.ItemDataRole.UserRole, str(entry.path))
            self.list.addItem(item)

    def _launch(self, item: QListWidgetItem) -> None:
        subprocess.Popen(["gio", "launch", item.data(Qt.ItemDataRole.UserRole)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


class BrowserPage(QWidget):
    ask = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        page, col = _page("globe", "Browser",
                          "Open a site in your browser, or let Jarvis research the web and "
                          "answer from real pages.")
        QVBoxLayout(self).addWidget(page)
        self.layout().setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setObjectName("field")
        self.url.setPlaceholderText("youtube.com or a search like “best laptops 2026”")
        go = _button("Open", icon_name="external")
        research = _button("Ask Jarvis to research", "secondary", "search")
        row.addWidget(self.url, 1)
        row.addWidget(go)
        row.addWidget(research)
        col.addLayout(row)
        quick = QHBoxLayout()
        for name, url in [("YouTube", "https://www.youtube.com"), ("Gmail", "https://mail.google.com"),
                          ("GitHub", "https://github.com"), ("Wikipedia", "https://wikipedia.org")]:
            b = QPushButton(name)
            b.setObjectName("chip")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
            quick.addWidget(b)
        quick.addStretch(1)
        col.addLayout(quick)
        col.addStretch(1)
        go.clicked.connect(self._open)
        self.url.returnPressed.connect(self._open)
        research.clicked.connect(
            lambda: self.url.text().strip() and self.ask.emit(
                f"Search the web for {self.url.text().strip()} and summarize what you find"))

    def _open(self) -> None:
        text = self.url.text().strip()
        if not text:
            return
        if " " in text or "." not in text:
            url = QUrl("https://search.brave.com/search")
            from PySide6.QtCore import QUrlQuery

            query = QUrlQuery()
            query.addQueryItem("q", text)
            url.setQuery(query)
        else:
            url = QUrl(text if "://" in text else f"https://{text}")
        QDesktopServices.openUrl(url)


class HistoryPage(QWidget):
    rerun = Signal(str)

    def __init__(self, repository) -> None:
        super().__init__()
        page, col = _page("history", "Task History",
                          "Everything Jarvis has done, newest first. Select a task to see its "
                          "steps; double-click to run it again.")
        QVBoxLayout(self).addWidget(page)
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.repository = repository
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Request", "Status", "When"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(300)
        col.addWidget(self.table, 1)
        self.details = QTextBrowser()
        self.details.setMinimumHeight(200)
        col.addWidget(self.details)
        self.tasks: list[Task] = []
        self.table.itemSelectionChanged.connect(self._show_details)
        self.table.itemDoubleClicked.connect(
            lambda item: self.rerun.emit(self.tasks[item.row()].user_prompt))

    def showEvent(self, event) -> None:  # noqa: N802
        self.refresh()
        super().showEvent(event)

    def refresh(self) -> None:
        self.tasks = self.repository.recent(200) if self.repository else []
        self.table.setRowCount(len(self.tasks))
        for row, task in enumerate(self.tasks):
            text, color, _ = STATUS_BADGE.get(task.status, ("Running", theme.ACCENT_HI, ""))
            status = QTableWidgetItem(text)
            status.setForeground(Qt.GlobalColor.white)
            from PySide6.QtGui import QColor

            status.setForeground(QColor(color))
            self.table.setItem(row, 0, QTableWidgetItem(task.user_prompt))
            self.table.setItem(row, 1, status)
            self.table.setItem(row, 2, QTableWidgetItem(relative_time(task.created_at)))

    def _show_details(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        task = self.tasks[rows[0].row()]
        parts = [f"<h3>{html.escape(task.user_prompt)}</h3>"]
        for step in task.steps:
            mark = {TaskStatus.COMPLETED: "✓", TaskStatus.FAILED: "✗",
                    TaskStatus.CANCELLED: "–"}.get(step.status, "…")
            err = (f" <span style='color:{theme.RED}'>({html.escape(step.result.error)})</span>"
                   if step.result and step.result.error else "")
            parts.append(f"<p>{mark} {html.escape(step.description)}{err}</p>")
        if task.final_response:
            parts.append(f"<p><b>Jarvis:</b> {html.escape(task.final_response)}</p>")
        self.details.setHtml("".join(parts))


class SettingsPage(QWidget):
    def __init__(self, settings, config_path: Path | None, header) -> None:
        super().__init__()
        page, col = _page("settings", "Settings",
                          "Quick switches here; everything else lives in the settings file.")
        QVBoxLayout(self).addWidget(page)
        self.layout().setContentsMargins(0, 0, 0, 0)

        self.speak = QCheckBox("Speak replies aloud")
        self.hands_free = QCheckBox("Hands-free listening inside the app (“Jarvis, …”)")
        # Mirror the header toggles, which own the state.
        for box, toggle in ((self.speak, header.voice_toggle),
                            (self.hands_free, header.mic_toggle)):
            box.setChecked(toggle.isChecked())
            box.toggled.connect(toggle.setChecked)
            toggle.toggled.connect(box.setChecked)
            col.addWidget(box)

        info = [
            ("Language model", f"{settings.ollama.model}"
                               f"{' (thinking on)' if settings.ollama.think else ''}"),
            ("Vision model", settings.desktop.vision_model),
            ("Speech recognition", f"faster-whisper {settings.voice.stt_model}"),
            ("Voice", Path(settings.voice.tts_voice).stem),
            ("Allowed folders", ", ".join(settings.permissions.filesystem.allowed_paths)),
            ("Protected apps (always ask)",
             ", ".join(settings.permissions.desktop.protected_applications)),
            ("Wake word", "“Hey Jarvis” — background listener starts at login"),
        ]
        rows = "".join(f"<tr><td style='color:{theme.MUTED};padding:4px 16px 4px 0'>{k}</td>"
                       f"<td style='padding:4px 0'>{html.escape(str(v))}</td></tr>"
                       for k, v in info)
        table = QTextBrowser()
        table.setHtml(f"<table>{rows}</table>")
        table.setMinimumHeight(240)
        col.addWidget(table)

        buttons = QHBoxLayout()
        if config_path:
            edit = _button("Open settings file", icon_name="file")
            edit.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(config_path))))
            buttons.addWidget(edit)
            buttons.addWidget(label("Restart Jarvis after editing.", "dim"))
        buttons.addStretch(1)
        col.addLayout(buttons)
        col.addStretch(1)
