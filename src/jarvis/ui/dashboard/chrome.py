"""Window chrome: the custom title/header bar and the navigation sidebar."""

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jarvis.ui.dashboard import theme
from jarvis.ui.dashboard.icons import icon, pixmap
from jarvis.ui.dashboard.widgets import StatusDot, label


class Logo(QWidget):
    """The circled "J" mark."""

    def __init__(self, size: int = 44):
        super().__init__()
        self.setFixedSize(size, size)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self.width()
        p.setPen(QPen(QColor(theme.ACCENT), max(2, s // 14)))
        p.drawArc(3, 3, s - 6, s - 6, 60 * 16, 300 * 16)
        p.setPen(QColor(theme.TEXT))
        font = QFont(self.font())
        font.setPixelSize(int(s * 0.5))
        font.setBold(True)
        p.setFont(font)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "J")
        p.setBrush(QColor(theme.ACCENT))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(s * 0.82, s * 0.2), s * 0.06, s * 0.06)


class StatusToggle(QToolButton):
    """Header item like "Microphone · On": an icon plus a two-line label; clickable."""

    def __init__(self, icon_on: str, icon_off: str, title: str, checkable: bool = True):
        super().__init__()
        self.setObjectName("statusToggle")
        self.icon_on, self.icon_off, self.title = icon_on, icon_off, title
        self.setCheckable(checkable)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setIconSize(QSize(22, 22))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggled.connect(self._refresh)
        self._refresh(self.isChecked())

    def _refresh(self, on: bool) -> None:
        color = theme.ACCENT_HI if on else theme.DIM
        self.setIcon(icon(self.icon_on if on else self.icon_off, color, 22))
        self.setText(f"{self.title}\n{'On' if on else 'Off'}")
        self.setStyleSheet(f"QToolButton {{ color: {theme.TEXT}; font-size: 13px; }}")


class Header(QFrame):
    settings_clicked = Signal()

    def __init__(self, window: QWidget):
        super().__init__()
        self.setObjectName("header")
        self.window_ = window
        self.setFixedHeight(76)
        row = QHBoxLayout(self)
        row.setContentsMargins(22, 8, 14, 8)
        row.setSpacing(14)

        row.addWidget(Logo())
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(label("JARVIS", "appTitle"))
        titles.addWidget(label("Your Local Desktop AI Assistant", "muted"))
        row.addLayout(titles)
        row.addStretch(1)

        self.llm_dot = StatusDot(theme.AMBER)
        self.llm_text = label("Local LLM\nConnecting…")
        self.llm_text.setStyleSheet("font-size: 13px;")
        llm = QHBoxLayout()
        llm.setSpacing(8)
        llm.addWidget(self.llm_dot)
        llm.addWidget(self.llm_text)
        row.addLayout(llm)
        row.addSpacing(18)

        self.mic_toggle = StatusToggle("mic", "mic-off", "Microphone")
        self.mic_toggle.setToolTip("Hands-free listening inside the app (say “Jarvis, …”)")
        self.voice_toggle = StatusToggle("volume", "volume-off", "Voice")
        self.voice_toggle.setToolTip("Speak replies aloud")
        row.addWidget(self.mic_toggle)
        row.addWidget(self.voice_toggle)
        row.addSpacing(10)

        divider = QFrame()
        divider.setFixedSize(1, 40)
        divider.setStyleSheet(f"background: {theme.BORDER};")
        settings = self._window_button("settings", "Settings")
        settings.clicked.connect(self.settings_clicked.emit)
        row.addWidget(settings)
        row.addWidget(divider)
        minimize = self._window_button("minus", "Minimize")
        minimize.clicked.connect(window.showMinimized)
        self.maximize = self._window_button("square", "Maximize")
        self.maximize.clicked.connect(self._toggle_maximized)
        close = self._window_button("x", "Close", "closeButton")
        close.clicked.connect(window.close)
        for button in (minimize, self.maximize, close):
            row.addWidget(button)

    def _window_button(self, name: str, tip: str, object_name: str = "windowButton") -> QToolButton:
        button = QToolButton()
        button.setObjectName(object_name)
        button.setIcon(icon(name, theme.TEXT, 20))
        button.setIconSize(QSize(20, 20))
        button.setToolTip(tip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def _toggle_maximized(self) -> None:
        if self.window_.isMaximized():
            self.window_.showNormal()
        else:
            self.window_.showMaximized()

    def set_llm_status(self, ok: bool, text: str) -> None:
        self.llm_dot.set_color(theme.GREEN if ok else theme.RED)
        self.llm_text.setText(text)

    # Drag the frameless window by its header; double-click maximizes.
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window_.windowHandle()
            if handle is not None:
                handle.startSystemMove()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self._toggle_maximized()


NAV_ITEMS = [
    ("home", "Home", "home"),
    ("files", "File Manager", "folder"),
    ("apps", "Applications", "grid"),
    ("browser", "Browser", "globe"),
    ("history", "Task History", "history"),
    ("settings", "Settings", "settings"),
]


class Sidebar(QFrame):
    page_selected = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("sidebar")
        self.setFixedWidth(200)
        col = QVBoxLayout(self)
        col.setContentsMargins(10, 12, 10, 14)
        col.setSpacing(6)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QPushButton] = {}
        for key, text, icon_name in NAV_ITEMS:
            button = QPushButton(f"  {text}")
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setIcon(icon(icon_name, theme.TEXT, 20))
            button.setIconSize(QSize(20, 20))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, k=key: self.page_selected.emit(k))
            self.group.addButton(button)
            self.buttons[key] = button
            col.addWidget(button)
        col.addStretch(1)

        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {theme.BORDER};")
        col.addWidget(line)
        brand = QHBoxLayout()
        brand.setSpacing(8)
        brand.addWidget(Logo(26))
        brand.addWidget(label("Jarvis"))
        brand.addStretch(1)
        col.addLayout(brand)
        tagline = label("Think  ·  Plan  ·  Act", "muted")
        tagline.setStyleSheet(f"color: {theme.MUTED}; font-size: 12px;")
        col.addWidget(tagline)
        col.addWidget(label("All local. No cloud. No limits.", "dim"))
        self.select("home")

    def select(self, key: str) -> None:
        self.buttons[key].setChecked(True)


class FooterStatus(QWidget):
    """Status bar contents: coloured dot + message."""

    def __init__(self) -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 2, 12, 2)
        row.setSpacing(8)
        self.dot = StatusDot(theme.GREEN, 9)
        self.text = QLabel("Jarvis is ready")
        self.text.setStyleSheet(f"color: {theme.MUTED};")
        row.addWidget(self.dot)
        row.addWidget(self.text)

    def set(self, text: str, color: str = theme.GREEN) -> None:
        self.text.setText(text)
        self.dot.set_color(color)


def nav_icon(name: str) -> QLabel:  # small helper for pages
    widget = QLabel()
    widget.setPixmap(pixmap(name, theme.ACCENT_HI, 22))
    return widget
