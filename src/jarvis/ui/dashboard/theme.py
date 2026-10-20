"""Dark navy dashboard theme: palette and the application stylesheet."""

BG = "#070d1a"          # window background
PANEL = "#0c1528"       # cards
PANEL_2 = "#101b33"     # raised elements inside cards (inputs, list rows)
BORDER = "#1b2944"
BORDER_HI = "#2a3d63"
TEXT = "#e6edf7"
MUTED = "#8a9bb8"
DIM = "#5f7092"
ACCENT = "#2f6df6"
ACCENT_HI = "#4d86ff"
ACCENT_SOFT = "#16305f"
GREEN = "#22c55e"
GREEN_SOFT = "#0f3a26"
AMBER = "#f5a524"
AMBER_SOFT = "#3d2c0c"
RED = "#ef4444"
RED_SOFT = "#3b1316"
CYAN = "#38bdf8"

STYLESHEET = f"""
* {{
    font-family: "Inter", "Cantarell", "Ubuntu", sans-serif;
    color: {TEXT};
}}
QMainWindow, QWidget#root {{ background: {BG}; }}
QToolTip {{ background: {PANEL_2}; color: {TEXT}; border: 1px solid {BORDER_HI}; padding: 4px; }}

QFrame#card {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 14px;
}}
QFrame#subcard {{
    background: {PANEL_2};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
QLabel#cardTitle {{ font-size: 16px; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#dim {{ color: {DIM}; font-size: 12px; }}
QLabel#appTitle {{ font-size: 22px; font-weight: 700; letter-spacing: 2px; }}
QLabel#greeting {{ font-size: 30px; font-weight: 600; }}
QLabel#subGreeting {{ font-size: 16px; color: #c3cee2; }}

/* header */
QFrame#header {{ background: {BG}; border-bottom: 1px solid {BORDER}; }}
QToolButton#windowButton {{
    background: transparent; border: none; border-radius: 6px; padding: 6px;
}}
QToolButton#windowButton:hover {{ background: {PANEL_2}; }}
QToolButton#closeButton {{
    background: transparent; border: none; border-radius: 6px; padding: 6px;
}}
QToolButton#closeButton:hover {{ background: {RED}; }}
QToolButton#statusToggle {{
    background: transparent; border: none; border-radius: 8px; padding: 4px 8px;
    text-align: left;
}}
QToolButton#statusToggle:hover {{ background: {PANEL_2}; }}

/* sidebar */
QFrame#sidebar {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 14px; }}
QPushButton#navButton {{
    text-align: left; padding: 11px 14px; border: none; border-radius: 10px;
    font-size: 15px; background: transparent; color: #cfd8e8;
}}
QPushButton#navButton:hover {{ background: {PANEL_2}; }}
QPushButton#navButton:checked {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2459d6, stop:1 #1b3f96);
    color: white;
}}

/* command bar */
QFrame#commandBar {{
    background: {PANEL_2};
    border: 1px solid {ACCENT};
    border-radius: 30px;
}}
QLineEdit#commandInput {{
    background: transparent; border: none; font-size: 16px; color: {TEXT};
    selection-background-color: {ACCENT};
}}
QPushButton#micButton {{
    background: {ACCENT}; border: none; border-radius: 22px;
}}
QPushButton#micButton:hover {{ background: {ACCENT_HI}; }}
QPushButton#micButton:checked {{ background: {RED}; }}
QPushButton#sendButton {{
    background: #1a2747; border: 1px solid {BORDER_HI}; border-radius: 20px;
}}
QPushButton#sendButton:hover {{ background: #22335c; }}
QPushButton#stopButton {{
    background: {RED_SOFT}; border: 1px solid {RED}; border-radius: 20px;
}}
QPushButton#chip {{
    background: {PANEL_2}; border: 1px solid {BORDER_HI}; border-radius: 15px;
    padding: 6px 13px; font-size: 13px; color: #cfd8e8;
}}
QPushButton#chip:hover {{ border-color: {ACCENT}; color: white; }}

/* feature cards */
QPushButton#featureCard {{
    background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 12px;
    padding: 14px; text-align: center;
}}
QPushButton#featureCard:hover {{ border-color: {ACCENT}; background: #13213e; }}

QPushButton#linkButton {{
    background: transparent; border: none; color: {ACCENT_HI}; font-size: 13px;
}}
QPushButton#linkButton:hover {{ color: white; }}

QPushButton#primary {{
    background: #16a34a; border: none; border-radius: 8px; padding: 10px; font-weight: 600;
}}
QPushButton#primary:hover {{ background: {GREEN}; }}
QPushButton#secondary {{
    background: {PANEL_2}; border: 1px solid {BORDER_HI}; border-radius: 8px; padding: 10px;
}}
QPushButton#secondary:hover {{ border-color: {ACCENT}; }}
QPushButton#accentButton {{
    background: {ACCENT}; border: none; border-radius: 8px; padding: 8px 14px; font-weight: 600;
}}
QPushButton#accentButton:hover {{ background: {ACCENT_HI}; }}

QLabel#badgeGreen {{
    background: {GREEN_SOFT}; color: {GREEN}; border-radius: 9px; padding: 3px 9px;
    font-size: 12px; font-weight: 600;
}}
QLabel#badgeAmber {{
    background: {AMBER_SOFT}; color: {AMBER}; border-radius: 9px; padding: 3px 9px;
    font-size: 12px; font-weight: 600;
}}
QLabel#badgeGrey {{
    background: {PANEL_2}; color: {MUTED}; border-radius: 9px; padding: 3px 9px;
    font-size: 12px; font-weight: 600;
}}

QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER_HI}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER_HI}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

QLineEdit#field {{
    background: {PANEL_2}; border: 1px solid {BORDER_HI}; border-radius: 8px;
    padding: 8px 10px; font-size: 14px;
}}
QLineEdit#field:focus {{ border-color: {ACCENT}; }}
QComboBox {{
    background: {PANEL_2}; border: 1px solid {BORDER_HI}; border-radius: 8px; padding: 6px 10px;
}}
QComboBox QAbstractItemView {{ background: {PANEL_2}; selection-background-color: {ACCENT}; }}
QTreeView, QListWidget, QTableWidget {{
    background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 10px;
    alternate-background-color: #0e1830; gridline-color: {BORDER};
    selection-background-color: {ACCENT_SOFT}; selection-color: white;
}}
QHeaderView::section {{
    background: {PANEL}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER};
    padding: 6px;
}}
QTextBrowser {{ background: transparent; border: none; font-size: 14px; }}
QCheckBox {{ spacing: 10px; font-size: 14px; }}
QCheckBox::indicator {{
    width: 38px; height: 20px; border-radius: 10px; background: {BORDER_HI};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; }}

QStatusBar {{ background: {BG}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QStatusBar::item {{ border: none; }}
"""
