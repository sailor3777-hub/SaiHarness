"""Themes: QSS for the window plus the colors the code sets itself (chat HTML, tree rows).

The theme is stored in app_settings (key theme). current() returns the active theme; apply() applies it to the app and
notifies subscribers through bus.changed so that the chat and panels are recolored without a restart.
"""
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from core import db

SETTING_KEY = "theme"
UI_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Theme:
    key: str
    title: str
    qss_file: str
    user: str          # the "You" label
    assistant: str     # the "Model" label
    system: str
    reasoning: str     # reasoning and muted text
    tool: str
    tool_error: str
    dim: str           # unavailable and service tree rows
    normal: str        # the usual color of a row with tokens
    warn: str
    over: str
    code_bg: str
    code_fg: str
    inline_code: str
    link: str
    table_head: str
    quote: str

    def qss(self) -> str:
        return (UI_DIR / self.qss_file).read_text(encoding="utf-8")

    def richtext_css(self) -> str:
        """Styles for QTextDocument.setDefaultStyleSheet: Qt understands only simple tag selectors."""
        return (
            f"pre {{ background-color: {self.code_bg}; color: {self.code_fg}; font-family: Consolas, monospace; }}\n"
            f"code {{ background-color: {self.code_bg}; color: {self.inline_code}; font-family: Consolas, monospace; }}\n"
            f"a {{ color: {self.link}; }}\n"
            f"th {{ background-color: {self.table_head}; }}\n"
            f"blockquote {{ color: {self.quote}; }}\n"
        )


DARK = Theme(
    "dark", "Dark", "theme.qss",
    user="#8ab4f8", assistant="#81c995", system="#aaaaaa", reasoning="#8a8a8a", tool="#d7ba7d", tool_error="#ff6b6b",
    dim="#666666", normal="#8a8a8a", warn="#ffd54a", over="#ff6b6b",
    code_bg="#141414", code_fg="#e6e6e6", inline_code="#f0c674", link="#8ab4f8", table_head="#1c1c1c", quote="#aaaaaa",
)
LIGHT = Theme(
    "light", "Light", "theme_light.qss",
    user="#1a56c4", assistant="#1b7a3a", system="#666666", reasoning="#777777", tool="#8a6200", tool_error="#c62828",
    dim="#9a9a9a", normal="#666666", warn="#b26a00", over="#c62828",
    code_bg="#eceff1", code_fg="#202124", inline_code="#a0521d", link="#1a56c4", table_head="#e3e6ea", quote="#555555",
)
DIABLO = Theme(
    "diablo", "Diablo", "theme_diablo.qss",
    user="#e0b04a", assistant="#e8705a", system="#a89870", reasoning="#8a7a5a", tool="#c9a24b", tool_error="#ff5a3c",
    dim="#6a5a40", normal="#8a7650", warn="#e0b04a", over="#ff4a3a",
    code_bg="#1a0f0a", code_fg="#e8d9b5", inline_code="#e0b04a", link="#e0a040", table_head="#2a130e", quote="#a89870",
)
THEMES = {t.key: t for t in (DARK, LIGHT, DIABLO)}
DEFAULT_KEY = DARK.key


class _Bus(QObject):
    changed = Signal(object)  # the new Theme


bus = _Bus()
_current: Theme = DARK


def current() -> Theme:
    return _current


def saved_key() -> str:
    key = db.get_setting(SETTING_KEY, DEFAULT_KEY)
    return key if key in THEMES else DEFAULT_KEY


def apply(key: str, save: bool = True) -> Theme:
    """Makes a theme active: the app style, the database setting, a signal to subscribers."""
    global _current
    _current = THEMES.get(key, DARK)
    app = QApplication.instance()
    if app is not None:
        app.setStyleSheet(_current.qss())
    if save:
        db.set_setting(SETTING_KEY, _current.key)
    bus.changed.emit(_current)
    return _current
