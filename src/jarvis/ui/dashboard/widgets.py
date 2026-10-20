"""Reusable dashboard pieces: cards, badges, the animated orb and waveforms."""

import math

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QConicalGradient, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from jarvis.ui.dashboard import theme
from jarvis.ui.dashboard.icons import pixmap

# Activity states shared by the orb, waveforms and the Current Activity card.
IDLE, LISTENING, THINKING, SPEAKING = "idle", "listening", "thinking", "speaking"


def card(object_name: str = "card") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName(object_name)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 16)
    layout.setSpacing(12)
    return frame, layout


def label(text: str = "", name: str | None = None, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def icon_label(name: str, color: str, size: int = 20) -> QLabel:
    widget = QLabel()
    widget.setPixmap(pixmap(name, color, size))
    widget.setFixedSize(size + 4, size + 4)
    widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return widget


class IconBadge(QLabel):
    """Rounded square (or circle) tile with a coloured icon, as in the task lists."""

    def __init__(self, name: str, color: str, background: str, size: int = 40,
                 round_: bool = False):
        super().__init__()
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        radius = size // 2 if round_ else 10
        self.setStyleSheet(f"background: {background}; border-radius: {radius}px;")
        self.set_icon(name, color, size)

    def set_icon(self, name: str, color: str, size: int | None = None) -> None:
        self.setPixmap(pixmap(name, color, int((size or self.width()) * 0.5)))


def badge(text: str, kind: str = "Green") -> QLabel:
    widget = QLabel(text)
    widget.setObjectName(f"badge{kind}")
    widget.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return widget


def card_header(icon_name: str, title: str, right: QWidget | None = None,
                color: str = theme.ACCENT_HI) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(10)
    row.addWidget(icon_label(icon_name, color, 22))
    row.addWidget(label(title, "cardTitle"))
    row.addStretch(1)
    if right is not None:
        row.addWidget(right)
    return row


class StatusDot(QWidget):
    def __init__(self, color: str = theme.GREEN, size: int = 9):
        super().__init__()
        self._color = QColor(color)
        self.setFixedSize(size, size)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(self.rect())


class _Animated(QWidget):
    """Base for widgets that animate while Jarvis is active."""

    def __init__(self) -> None:
        super().__init__()
        self.state = IDLE
        self._t = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)

    def set_state(self, state: str) -> None:
        self.state = state
        self.update()

    def _tick(self) -> None:
        self._t += 0.04
        if self.isVisible():
            self.update()

    def energy(self) -> float:
        return {IDLE: 0.25, LISTENING: 0.85, THINKING: 0.55, SPEAKING: 1.0}[self.state]


class Waveform(_Animated):
    """Vertical bars that ripple; idle bars barely move."""

    def __init__(self, bars: int = 11, color: str = theme.ACCENT_HI, mirrored: bool = False,
                 bar_width: float = 3.0, gap: float = 5.0, height: int = 70):
        super().__init__()
        self.bars, self.color, self.mirrored = bars, QColor(color), mirrored
        self.bar_width, self.gap = bar_width, gap
        width = int(bars * bar_width + (bars - 1) * gap) + 2
        self.setFixedSize(width, height)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        h, mid = self.height(), self.height() / 2
        e = self.energy()
        for i in range(self.bars):
            k = (self.bars - 1 - i) if self.mirrored else i
            # Taller towards the orb, with a travelling ripple.
            base = 0.25 + 0.75 * (k / max(1, self.bars - 1))
            wobble = 0.5 + 0.5 * math.sin(self._t * (3 + e * 4) + i * 0.9)
            amp = max(0.12, base * (0.35 + 0.65 * wobble * e))
            bar_h = h * 0.9 * amp
            x = i * (self.bar_width + self.gap) + 1
            color = QColor(self.color)
            color.setAlphaF(0.35 + 0.6 * base)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(x, mid - bar_h / 2, self.bar_width, bar_h), 1.5, 1.5)


class Orb(_Animated):
    """Glowing ring with a monitor glyph: the hero element on the Home page."""

    def __init__(self, size: int = 120):
        super().__init__()
        self.setFixedSize(size, size)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.width() / 2
        c = QPointF(r, r)
        pulse = 0.5 + 0.5 * math.sin(self._t * (2.0 + 3 * self.energy()))
        glow = QRadialGradient(c, r)
        glow.setColorAt(0.55, QColor(47, 109, 246, int(40 + 60 * pulse * self.energy())))
        glow.setColorAt(1.0, QColor(47, 109, 246, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(c, r, r)

        ring = QConicalGradient(c, -self._t * 60 * (0.3 + self.energy()))
        ring.setColorAt(0.0, QColor(theme.CYAN))
        ring.setColorAt(0.5, QColor(theme.ACCENT))
        ring.setColorAt(1.0, QColor(theme.CYAN))
        p.setPen(QPen(QBrush(ring), 5))
        p.setBrush(QColor(theme.BG))
        inner = r * 0.78
        p.drawEllipse(c, inner, inner)

        p.drawPixmap(int(r - 22), int(r - 22), pixmap("monitor", "#dbe7ff", 44, 1.6))


class ActivityGlyph(_Animated):
    """Small waveform glyph for the Current Activity card."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(QSize(80, 46))

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        n, w, gap = 9, 4.0, 4.5
        x0 = (self.width() - (n * w + (n - 1) * gap)) / 2
        mid = self.height() / 2
        e = self.energy()
        for i in range(n):
            centre = 1 - abs(i - (n - 1) / 2) / ((n - 1) / 2)
            wave = 0.5 + 0.5 * math.sin(self._t * (2 + 6 * e) + i * 1.1)
            h = self.height() * (0.2 + 0.75 * centre * (0.4 + 0.6 * wave * e))
            color = QColor(theme.ACCENT_HI if i % 2 else theme.CYAN)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(x0 + i * (w + gap), mid - h / 2, w, h), 2, 2)


class Separator(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(1)
        self.setStyleSheet(f"background: {theme.BORDER}; border: none;")


class FlowLayout(QLayout):
    """Lays items out left to right, wrapping to new rows (for chips)."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 10):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802 - Qt API
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _layout(self, rect, apply: bool) -> int:
        x, y, line_h = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > rect.right() + 1 and line_h > 0:
                x, y = rect.x(), y + line_h + self._spacing
                line_h = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


class ElidedLabel(QLabel):
    """Single-line label that shortens long text with "…" instead of widening the layout."""

    def __init__(self, text: str = "", name: str | None = None):
        super().__init__()
        if name:
            self.setObjectName(name)
        self._full = text
        self.setToolTip(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)
        super().setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API
        self._full = text
        self.setToolTip(text)
        self._elide()

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._elide()
        super().resizeEvent(event)

    def _elide(self) -> None:
        metrics = self.fontMetrics()
        super().setText(metrics.elidedText(self._full, Qt.TextElideMode.ElideMiddle,
                                           max(10, self.width())))
