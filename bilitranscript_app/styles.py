from __future__ import annotations


COLORS = {
    "background": "#101216",
    "surface": "#171A20",
    "surface_raised": "#1D2129",
    "border": "#2B303A",
    "text": "#F4F6FA",
    "muted": "#969EAC",
    "faint": "#6F7785",
    "accent": "#FB7299",
    "accent_hover": "#FF89AA",
    "accent_pressed": "#E75E87",
    "success": "#45D1A3",
    "warning": "#F3BE62",
    "danger": "#FF6E72",
}


APP_STYLE = f"""
* {{
    font-family: "Segoe UI", "Microsoft YaHei UI";
    font-size: 13px;
    color: {COLORS['text']};
}}
QMainWindow, QDialog, QWidget#appRoot {{
    background: {COLORS['background']};
}}
QToolTip {{
    color: {COLORS['text']};
    background: {COLORS['surface_raised']};
    border: 1px solid {COLORS['border']};
    padding: 6px;
}}
QFrame#card {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 14px;
}}
QFrame#softPanel {{
    background: {COLORS['surface_raised']};
    border: 1px solid {COLORS['border']};
    border-radius: 10px;
}}
QLabel#appTitle {{
    font-size: 20px;
    font-weight: 700;
}}
QLabel#appSubtitle, QLabel#muted, QLabel#meta {{
    color: {COLORS['muted']};
}}
QLabel#sectionTitle {{
    font-size: 14px;
    font-weight: 650;
}}
QLabel#videoTitle {{
    font-size: 17px;
    font-weight: 700;
}}
QLabel#metric {{
    color: {COLORS['muted']};
    padding: 5px 9px;
    background: {COLORS['surface_raised']};
    border: 1px solid {COLORS['border']};
    border-radius: 10px;
}}
QLabel#privacyPill {{
    color: {COLORS['success']};
    background: rgba(69, 209, 163, 0.10);
    border: 1px solid rgba(69, 209, 163, 0.30);
    border-radius: 11px;
    padding: 5px 10px;
}}
QLineEdit, QComboBox, QPlainTextEdit, QListWidget, QSpinBox {{
    background: {COLORS['surface_raised']};
    border: 1px solid {COLORS['border']};
    border-radius: 9px;
    padding: 8px 10px;
    selection-background-color: {COLORS['accent']};
}}
QLineEdit {{
    font-size: 14px;
    min-height: 26px;
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border: 1px solid {COLORS['accent']};
}}
QComboBox {{
    min-height: 24px;
    padding-right: 28px;
}}
QComboBox::drop-down {{
    border: none;
    width: 26px;
}}
QComboBox QAbstractItemView {{
    background: {COLORS['surface_raised']};
    border: 1px solid {COLORS['border']};
    selection-background-color: #3A2731;
    outline: none;
    padding: 4px;
}}
QPushButton {{
    min-height: 34px;
    border-radius: 9px;
    padding: 0 14px;
    font-weight: 600;
    background: {COLORS['surface_raised']};
    border: 1px solid {COLORS['border']};
}}
QPushButton:hover {{
    background: #242933;
    border-color: #3A414E;
}}
QPushButton:pressed {{
    background: #14171C;
}}
QPushButton:disabled {{
    color: {COLORS['faint']};
    background: #15181D;
    border-color: #242831;
}}
QPushButton#primaryButton {{
    color: #FFFFFF;
    background: {COLORS['accent']};
    border-color: {COLORS['accent']};
}}
QPushButton#primaryButton:hover {{
    background: {COLORS['accent_hover']};
    border-color: {COLORS['accent_hover']};
}}
QPushButton#primaryButton:pressed {{
    background: {COLORS['accent_pressed']};
}}
QPushButton#primaryButton:disabled {{
    color: #7D6570;
    background: #2A2025;
    border-color: #37262E;
}}
QPushButton#ghostButton {{
    background: transparent;
    border-color: transparent;
    color: {COLORS['muted']};
}}
QPushButton#ghostButton:hover {{
    background: {COLORS['surface_raised']};
    color: {COLORS['text']};
}}
QPushButton#dangerButton {{
    color: {COLORS['danger']};
    background: transparent;
    border-color: rgba(255, 110, 114, 0.35);
}}
QCheckBox {{
    spacing: 8px;
    color: {COLORS['muted']};
}}
QCheckBox::indicator {{
    width: 17px;
    height: 17px;
    border-radius: 5px;
    border: 1px solid #4A5260;
    background: {COLORS['surface_raised']};
}}
QCheckBox::indicator:hover {{
    border-color: {COLORS['accent']};
}}
QCheckBox::indicator:checked {{
    background: {COLORS['accent']};
    border-color: {COLORS['accent']};
}}
QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #3A404B;
    min-height: 28px;
    border-radius: 4px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
    height: 0;
}}
QPlainTextEdit#transcriptEditor {{
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 14px;
    line-height: 1.55;
    background: #13161B;
    padding: 15px;
}}
QListWidget::item {{
    padding: 6px 8px;
}}
QListWidget::item:selected {{
    background: #3A2731;
}}
QProgressBar {{
    min-height: 5px;
    max-height: 5px;
    border: none;
    background: {COLORS['surface_raised']};
    border-radius: 2px;
}}
QProgressBar::chunk {{
    background: {COLORS['accent']};
    border-radius: 2px;
}}
QSplitter::handle {{
    background: transparent;
    width: 10px;
}}
QMessageBox {{
    background: {COLORS['surface']};
}}
"""
