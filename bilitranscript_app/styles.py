from __future__ import annotations


LIGHT = {
    "background": "#EAF0F7", "surface": "rgba(255,255,255,0.66)",
    "surface_raised": "rgba(255,255,255,0.78)", "surface_hover": "rgba(255,255,255,0.92)",
    "content_tint": "rgba(245,248,253,0.54)", "sidebar_tint": "rgba(250,252,255,0.24)",
    "tooltip": "rgba(255,255,255,0.82)",
    "border": "rgba(77,96,124,0.25)", "text": "#162033", "muted": "#59677B", "faint": "#7C899B",
    "sidebar_text": "#2F405A", "sidebar_subtext": "#40536E", "accent_text": "#D94F7D",
    "accent": "#FB7299", "accent_hover": "#FF88AA", "accent_pressed": "#E55E87",
    "success": "#168C6B", "warning": "#A96C11", "danger": "#C64151",
    "editor": "rgba(255,255,255,0.58)", "selected": "rgba(251,114,153,0.16)",
}
DARK = {
    "background": "#090F18", "surface": "rgba(13,20,31,0.82)",
    "surface_raised": "rgba(23,33,48,0.90)", "surface_hover": "rgba(36,49,68,0.97)",
    "content_tint": "rgba(7,11,18,0.68)", "sidebar_tint": "rgba(14,21,32,0.34)",
    "tooltip": "rgba(23,33,48,0.84)",
    "border": "rgba(188,205,230,0.23)", "text": "#F7F9FD", "muted": "#C0CAD9", "faint": "#8D9AAF",
    "sidebar_text": "#D9E1ED", "sidebar_subtext": "#B9C5D6", "accent_text": "#FF8BAE",
    "accent": "#FB7299", "accent_hover": "#FF8EAE", "accent_pressed": "#E55E87",
    "success": "#54D2A6", "warning": "#F1C26B", "danger": "#FF7C86",
    "editor": "rgba(7,12,20,0.76)", "selected": "rgba(251,114,153,0.22)",
}
COLORS = DARK


def stylesheet(theme: str = "dark", *, acrylic: bool = True) -> str:
    is_light = str(theme).lower() == "light"
    colors = dict(LIGHT if is_light else DARK)
    if not acrylic:
        colors.update(
            {
                "surface": "#F4F7FB" if is_light else "#101824",
                "surface_raised": "#FFFFFF" if is_light else "#1A2636",
                "surface_hover": "#F8FAFD" if is_light else "#223147",
                "editor": "#FFFFFF" if is_light else "#0B121D",
            }
        )
    window_background = "transparent" if acrylic else colors["background"]
    root_background = "transparent" if acrylic else colors["background"]
    content_background = colors["content_tint"] if acrylic else colors["background"]
    sidebar_background = colors["sidebar_tint"] if acrylic else colors["surface"]
    return f"""
* {{ font-family: "Segoe UI", "Microsoft YaHei UI"; font-size: 13px; color: {colors['text']}; }}
QMainWindow {{ background: {window_background}; }} QDialog {{ background: {colors['background']}; }} QWidget#appRoot {{ background: {root_background}; }}
QStackedWidget, QWidget#homePage, QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }} QStackedWidget#contentArea {{ background: {content_background}; }}
QToolTip {{ color: {colors['text']}; background: {colors['surface_hover']}; border: 1px solid {colors['border']}; padding: 6px; }}
QFrame#sidebar {{ background: {sidebar_background}; border-right: 1px solid {colors['border']}; }}
QFrame#card, QFrame#softPanel {{ background: {colors['surface']}; border: 1px solid {colors['border']}; border-radius: 16px; }}
QFrame#softPanel {{ background: {colors['surface_raised']}; border-radius: 12px; }}
QLabel#brandMark {{ color: white; background: {colors['accent']}; border-radius: 11px; font-size: 18px; font-weight: 800; }} QLabel#brandIcon {{ background: transparent; }}
QLabel#appTitle {{ font-size: 19px; font-weight: 720; }} QLabel#pageTitle {{ font-size: 25px; font-weight: 720; }} QLabel#contentPageTitle {{ font-size: 40px; font-weight: 740; }} QLabel#resultContentTitle {{ font-size: 33px; font-weight: 740; }}
QLabel#appSubtitle, QLabel#muted, QLabel#meta, QLabel#statusText {{ color: {colors['muted']}; }}
QLabel#sectionTitle {{ font-size: 15px; font-weight: 680; }} QLabel#videoTitle {{ font-size: 18px; font-weight: 700; }}
QWidget#settingsPage, QWidget#settingsForm {{ background: transparent; }} QLabel#settingsFieldLabel {{ color: {colors['muted']}; font-weight: 500; }} QLabel#settingsSectionTitle {{ font-size: 16px; font-weight: 680; }}
QLabel#metric {{ color: {colors['muted']}; padding: 5px 9px; background: {colors['surface_raised']}; border: 1px solid {colors['border']}; border-radius: 10px; }}
QLabel#successText {{ color: {colors['success']}; }} QLabel#errorText {{ color: {colors['danger']}; }}
QLineEdit, QComboBox, QPlainTextEdit, QListWidget, QSpinBox {{ color: {colors['text']}; background: {colors['surface_raised']}; border: 1px solid {colors['border']}; border-radius: 10px; padding: 8px 10px; selection-background-color: {colors['accent']}; }}
QLineEdit {{ min-height: 28px; }} QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QListWidget:focus, QSpinBox:focus {{ border: 1px solid {colors['accent']}; }}
QFrame#heroInputFrame {{ background: {colors['surface_hover']}; border: 1px solid {colors['border']}; border-radius: 33px; }}
QLineEdit#heroInput {{ font-size: 16px; min-height: 42px; padding: 0 6px; border: none; border-radius: 0; background: transparent; }} QLineEdit#heroInput:focus {{ border: none; }}
QComboBox {{ min-height: 27px; padding-right: 28px; }} QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox QAbstractItemView {{ color: {colors['text']}; background: {colors['surface_hover']}; border: 1px solid {colors['border']}; selection-background-color: {colors['selected']}; outline: none; padding: 4px; }}
QPushButton {{ min-height: 34px; border-radius: 10px; padding: 0 13px; font-weight: 600; background: {colors['surface_raised']}; border: 1px solid {colors['border']}; }}
QPushButton:hover {{ background: {colors['surface_hover']}; border-color: {colors['accent']}; }} QPushButton:pressed {{ background: {colors['selected']}; }}
QPushButton:disabled {{ color: {colors['faint']}; }} QPushButton#primaryButton {{ color: #FFFFFF; background: {colors['accent']}; border-color: {colors['accent']}; }}
QPushButton#primaryButton:hover {{ background: {colors['accent_hover']}; border-color: {colors['accent_hover']}; }} QPushButton#primaryButton:pressed {{ background: {colors['accent_pressed']}; }}
GlyphButton#heroSubmit {{ color: #FFFFFF; background: {colors['accent']}; border: none; border-radius: 26px; padding: 0; }} GlyphButton#heroSubmit:hover {{ background: {colors['accent_hover']}; }} GlyphButton#heroSubmit:pressed {{ background: {colors['accent_pressed']}; }}
QFrame#windowChrome, QWidget#titleDragArea {{ background: transparent; border: none; }} GlyphButton#chromeButton, GlyphButton#chromeCloseButton {{ background: transparent; border: none; border-radius: 0; padding: 0; min-height: 42px; font-size: 15px; font-weight: 400; }} GlyphButton#chromeButton:hover {{ background: {colors['surface_raised']}; }} GlyphButton#chromeCloseButton:hover {{ color: #FFFFFF; background: #C42B1C; }}
GlyphButton#iconButton {{ background: transparent; border: none; padding: 0; border-radius: 10px; }} GlyphButton#iconButton:hover {{ background: {colors['surface_raised']}; }}
QPushButton#ghostButton {{ background: transparent; border-color: transparent; color: {colors['muted']}; }} QPushButton#ghostButton:hover {{ background: {colors['surface_raised']}; color: {colors['text']}; }}
QPushButton#editorCornerButton {{ color: {colors['muted']}; background: {colors['surface_raised']}; border-color: {colors['border']}; }} QPushButton#editorCornerButton:hover {{ color: {colors['text']}; background: {colors['surface_hover']}; border-color: {colors['accent']}; }}
QPushButton#navButton {{ text-align: left; padding-left: 16px; background: transparent; border-color: transparent; color: {colors['sidebar_text']}; min-height: 39px; }} QPushButton#navButton:hover {{ background: {colors['surface_raised']}; color: {colors['text']}; }} QPushButton#navButton:checked {{ color: {colors['accent_text']}; background: {colors['selected']}; }}
QFrame#settingsDrawer {{ background: transparent; border: none; }} QPushButton#drawerButton {{ text-align: left; padding-left: 18px; min-height: 31px; background: transparent; border: none; color: {colors['sidebar_subtext']}; font-weight: 520; }} QPushButton#drawerButton:hover {{ color: {colors['text']}; background: {colors['surface_raised']}; }} QPushButton#drawerButton:checked {{ color: {colors['accent_text']}; background: transparent; }}
QPushButton#dangerButton {{ color: {colors['danger']}; background: transparent; border-color: rgba(198,65,81,0.35); }}
QCheckBox {{ spacing: 8px; color: {colors['muted']}; }} QCheckBox::indicator {{ width: 17px; height: 17px; border-radius: 5px; border: 1px solid {colors['border']}; background: {colors['surface_raised']}; }} QCheckBox::indicator:checked {{ background: {colors['accent']}; border-color: {colors['accent']}; }}
QScrollArea {{ border: none; background: transparent; }} QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }} QScrollBar::handle:vertical {{ background: {colors['border']}; min-height: 28px; border-radius: 4px; }} QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical, QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; height: 0; }}
QFrame#transcriptSurface {{ background: {colors['editor']}; border: 1px solid {colors['border']}; border-radius: 13px; }} QPlainTextEdit#transcriptEditor {{ font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 14px; background: transparent; padding: 0; border: none; border-radius: 0; }}
QLabel#timelineTooltip {{ color: {colors['text']}; background: {colors['tooltip']}; border: 1px solid {colors['border']}; border-radius: 9px; padding: 6px 9px; }}
QListWidget {{ padding: 6px; }} QListWidget::item {{ padding: 10px 9px; border-radius: 10px; }} QListWidget::item:hover {{ background: {colors['surface_hover']}; }} QListWidget::item:selected {{ background: {colors['selected']}; color: {colors['text']}; }}
QListWidget#historyList {{ background: transparent; border: none; padding: 0; outline: none; }} QListWidget#historyList::item {{ padding: 0; border: none; background: transparent; }}
QFrame#historyRow {{ background: {colors['surface_raised']}; border: 1px solid {colors['border']}; border-radius: 12px; }} QFrame#historyRow:hover {{ background: {colors['surface_hover']}; border-color: {colors['accent']}; }}
QListWidget#categoryList {{ background: transparent; border: none; padding: 2px; outline: none; }} QListWidget#categoryList:focus {{ border: none; }} QListWidget#categoryList::item {{ min-height: 28px; padding: 7px 12px; }}
QProgressBar {{ min-height: 5px; max-height: 5px; border: none; background: {colors['surface_raised']}; border-radius: 2px; }} QProgressBar::chunk {{ background: {colors['accent']}; border-radius: 2px; }}
QTabBar::tab {{ padding: 7px 14px; color: {colors['muted']}; border: none; }} QTabBar::tab:selected {{ color: {colors['accent']}; border-bottom: 2px solid {colors['accent']}; }}
QGroupBox {{ border: 1px solid {colors['border']}; border-radius: 12px; margin-top: 12px; padding: 12px; }} QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 5px; color: {colors['muted']}; }}
"""
