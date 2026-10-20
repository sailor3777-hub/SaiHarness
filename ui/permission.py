import itertools
import json
import textwrap
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

LINE_WIDTH = 100  # длиннее строку не показываем: окно подстраивается под самую длинную и вылезает за экран
VALUE_PREVIEW_MAX = 700  # сколько символов одного аргумента показывать в окне
ARG_LABELS = {  # понятные подписи аргументов для окна подтверждения
    "toolbox.fs_replace": {"path": "Файл", "old": "Было", "new": "Станет"},
    "toolbox.notes_write": {"name": "Заметка", "content": "Текст"},
    "toolbox.web_extract": {"url": "Адрес", "char_limit": "Лимит символов"},
    "toolbox.shell_run": {"command": "Команда PowerShell", "cwd": "Папка"},
    "toolbox.docx_write": {"path": "Файл", "markdown": "Текст (Markdown)", "overwrite": "Заменить существующий"},
    "toolbox.memory_save": {"topic": "Тема", "title": "Заголовок", "text": "Что запомнить"},
    "toolbox.memory_delete": {"id": "Номер записи"},
    "toolbox.xlsx_write": {"path": "Файл", "sheets": "Листы", "overwrite": "Заменить существующий"},
}
FULL_ARGUMENTS = {("toolbox.shell_run", "command")}  # команду показываем целиком: хвост нельзя прятать за «…»


def _wrap(text: str) -> str:
    """Длинные строки переносятся по словам (слова длиннее строки режутся); сам текст не меняется."""
    lines = []
    for line in text.split("\n"):
        lines.extend(textwrap.wrap(line, LINE_WIDTH, replace_whitespace=False, drop_whitespace=False) or [""])
    return "\n".join(lines)


def format_arguments(tool_id: str, arguments: dict) -> str:
    """Аргументы вызова для окна подтверждения: многострочные значения блоком, без JSON-экранирования."""
    labels = ARG_LABELS.get(tool_id, {})
    parts = []
    for key, value in arguments.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if len(text) > VALUE_PREVIEW_MAX and (tool_id, key) not in FULL_ARGUMENTS:
            text = text[:VALUE_PREVIEW_MAX] + f"… (ещё {len(text) - VALUE_PREVIEW_MAX} симв.)"
        label = labels.get(key, key)
        if "\n" in text or len(text) > 60:
            parts.append(f"{label}:\n" + "\n".join("    " + line for line in text.split("\n")))
        else:
            parts.append(f"{label}: {text}")
    return _wrap("\n\n".join(parts)) if parts else "(без аргументов)"


class PermissionBroker(QObject):
    """Мост между рабочим потоком и окном: поток просит подтверждение вызова и ждёт ответ пользователя."""

    asked = Signal(int, str, str)  # id запроса, инструмент, аргументы для показа

    def __init__(self, parent_widget=None, timeout: float = 300.0):
        super().__init__(parent_widget)
        self._parent_widget = parent_widget
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._pending: dict[int, list] = {}  # id -> [Event, ответ]
        self._lock = threading.Lock()
        self.asked.connect(self._on_asked)

    def ask(self, tool_id: str, arguments: dict, cancel: threading.Event | None = None) -> bool:
        """Из рабочего потока: True, если пользователь разрешил. Отмена генерации и тайм-аут дают отказ."""
        rid = next(self._ids)
        event = threading.Event()
        with self._lock:
            self._pending[rid] = [event, False]
        self.asked.emit(rid, tool_id, format_arguments(tool_id, arguments))
        waited = 0.0
        while not event.wait(0.2):
            waited += 0.2
            if (cancel is not None and cancel.is_set()) or waited >= self._timeout:
                break
        with self._lock:
            answer = self._pending.pop(rid, [None, False])[1]
        return bool(answer) and event.is_set()

    def _on_asked(self, rid: int, tool_id: str, text: str):
        """В главном потоке: показывает вопрос и передаёт ответ ждущему потоку."""
        allowed = False
        try:
            reply = QMessageBox.question(
                self._parent_widget, "Разрешить вызов инструмента?", f"{tool_id}\n\n{text}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            allowed = reply == QMessageBox.Yes
        finally:  # окно не открылось или упало: поток получает отказ сразу, а не ждёт таймаут
            with self._lock:
                entry = self._pending.get(rid)
                if entry is not None:
                    entry[1] = allowed
                    entry[0].set()
